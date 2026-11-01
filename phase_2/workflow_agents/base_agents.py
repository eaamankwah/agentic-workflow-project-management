"""
base_agents.py - Reusable agent library for agentic workflows.

Agents in this module:
    DirectPromptAgent            - relays a prompt straight to the LLM
    AugmentedPromptAgent         - answers using a persona
    KnowledgeAugmentedPromptAgent- answers using a persona + supplied knowledge only
    RAGKnowledgePromptAgent      - retrieval-augmented answers over a large text (provided)
    EvaluationAgent              - evaluates / iteratively refines a worker agent's answer
    ScoredEvaluationAgent        - EvaluationAgent variant using a 1-5 scoring rubric (enhancement)
    RoutingAgent                 - routes a prompt to the best agent by embedding similarity
    ActionPlanningAgent          - extracts an ordered list of steps from a prompt

The OpenAI API key is always passed in by the caller (never hard-coded here).
The OpenAI client honours the OPENAI_BASE_URL environment variable if you need
to point it at a proxy/compatible endpoint.
"""

import os
import re
import csv
import json
import uuid
from datetime import datetime

import numpy as np
import pandas as pd
from openai import OpenAI

CHAT_MODEL = "gpt-3.5-turbo"
EMBEDDING_MODEL = "text-embedding-3-large"

# Seconds to wait for a single OpenAI request before it is treated as timed out
# (the OpenAI SDK raises APITimeoutError). Override with OPENAI_REQUEST_TIMEOUT.
REQUEST_TIMEOUT_SECONDS = float(os.getenv("OPENAI_REQUEST_TIMEOUT", "60"))


def _make_client(api_key):
    """
    Create an OpenAI client that fails fast (instead of hanging) on slow requests.

    Also pre-flights a specific, common, 100%-predictable failure: a Vocareum/Udacity classroom
    key (format 'voc-...') will always be rejected with 401 by api.openai.com directly, it only
    authenticates through Vocareum's own proxy endpoint. Catching this here, before spending an
    API round trip, turns a generic AuthenticationError into a precise, actionable message, and
    protects every agent and every test script uniformly since they all call this one helper.
    """
    if api_key and api_key.strip().lower().startswith("voc-") and not os.getenv("OPENAI_BASE_URL"):
        raise RuntimeError(
            "This looks like a Vocareum/Udacity classroom API key (starts with 'voc-'), which "
            "only works through Vocareum's proxy endpoint, never directly against api.openai.com. "
            "Add this line to your .env file: OPENAI_BASE_URL=https://openai.vocareum.com/v1"
        )
    return OpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# DirectPromptAgent
# ---------------------------------------------------------------------------
class DirectPromptAgent:
    """Sends the user's prompt straight to the LLM (no system prompt, no context)."""

    def __init__(self, openai_api_key):
        self.openai_api_key = openai_api_key

    def respond(self, prompt):
        """Return only the text of the LLM's answer to `prompt`."""
        client = _make_client(self.openai_api_key)
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],  # user message only
            temperature=0,
        )
        return response.choices[0].message.content


# ---------------------------------------------------------------------------
# AugmentedPromptAgent
# ---------------------------------------------------------------------------
class AugmentedPromptAgent:
    """Answers prompts while assuming a predefined persona."""

    def __init__(self, openai_api_key, persona):
        self.openai_api_key = openai_api_key
        self.persona = persona

    def respond(self, input_text):
        """Return only the text of the persona-driven answer."""
        client = _make_client(self.openai_api_key)
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Assume the following persona and answer as that persona: {self.persona}\n"
                        "Forget all previous context."
                    ),
                },
                {"role": "user", "content": input_text},
            ],
            temperature=0,
        )
        return response.choices[0].message.content


# ---------------------------------------------------------------------------
# KnowledgeAugmentedPromptAgent
# ---------------------------------------------------------------------------
class KnowledgeAugmentedPromptAgent:
    """Answers prompts using a persona AND only the knowledge supplied to it."""

    def __init__(self, openai_api_key, persona, knowledge):
        self.persona = persona
        self.knowledge = knowledge
        self.openai_api_key = openai_api_key

    def respond(self, input_text):
        """Return only the text of the knowledge-grounded answer."""
        client = _make_client(self.openai_api_key)
        system_message = (
            f"You are {self.persona} knowledge-based assistant. Forget all previous context. "
            f"Use only the following knowledge to answer, do not use your own knowledge: {self.knowledge} "
            "Answer the prompt based on this knowledge, not your own."
        )
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": input_text},
            ],
            temperature=0,
        )
        return response.choices[0].message.content


# RAGKnowledgePromptAgent class definition
class RAGKnowledgePromptAgent:
    """
    An agent that uses Retrieval-Augmented Generation (RAG) to find knowledge from a large corpus
    and leverages embeddings to respond to prompts based solely on retrieved information.
    """

    def __init__(self, openai_api_key, persona, chunk_size=2000, chunk_overlap=100):
        """
        Initializes the RAGKnowledgePromptAgent with API credentials and configuration settings.

        Parameters:
        openai_api_key (str): API key for accessing OpenAI.
        persona (str): Persona description for the agent.
        chunk_size (int): The size of text chunks for embedding. Defaults to 2000.
        chunk_overlap (int): Overlap between consecutive chunks. Defaults to 100.
        """
        self.persona = persona
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.openai_api_key = openai_api_key
        self.unique_filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.csv"

    def get_embedding(self, text):
        """
        Fetches the embedding vector for given text using OpenAI's embedding API.

        Parameters:
        text (str): Text to embed.

        Returns:
        list: The embedding vector.
        """
        client = OpenAI(api_key=self.openai_api_key)
        response = client.embeddings.create(
            model="text-embedding-3-large",
            input=text,
            encoding_format="float"
        )
        return response.data[0].embedding

    def calculate_similarity(self, vector_one, vector_two):
        """
        Calculates cosine similarity between two vectors.

        Parameters:
        vector_one (list): First embedding vector.
        vector_two (list): Second embedding vector.

        Returns:
        float: Cosine similarity between vectors.
        """
        vec1, vec2 = np.array(vector_one), np.array(vector_two)
        return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))

    def chunk_text(self, text):
        """
        Splits text into manageable chunks, attempting natural breaks.

        Parameters:
        text (str): Text to split into chunks.

        Returns:
        list: List of dictionaries containing chunk metadata.
        """
        separator = "\n"
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) <= self.chunk_size:
            return [{"chunk_id": 0, "text": text, "chunk_size": len(text)}]

        chunks, start, chunk_id = [], 0, 0

        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            if separator in text[start:end]:
                end = start + text[start:end].rindex(separator) + len(separator)

            chunks.append({
                "chunk_id": chunk_id,
                "text": text[start:end],
                "chunk_size": end - start,
                "start_char": start,
                "end_char": end
            })

            # break the loop if we have reached the end of the text
            if end == len(text):
                break

            start = end - self.chunk_overlap
            chunk_id += 1

        with open(f"chunks-{self.unique_filename}", 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=["text", "chunk_size"])
            writer.writeheader()
            for chunk in chunks:
                writer.writerow({k: chunk[k] for k in ["text", "chunk_size"]})

        return chunks

    def calculate_embeddings(self):
        """
        Calculates embeddings for each chunk and stores them in a CSV file.

        Returns:
        DataFrame: DataFrame containing text chunks and their embeddings.
        """
        df = pd.read_csv(f"chunks-{self.unique_filename}", encoding='utf-8')
        df['embeddings'] = df['text'].apply(self.get_embedding)
        df.to_csv(f"embeddings-{self.unique_filename}", encoding='utf-8', index=False)
        return df

    def find_prompt_in_knowledge(self, prompt):
        """
        Finds and responds to a prompt based on similarity with embedded knowledge.

        Parameters:
        prompt (str): User input prompt.

        Returns:
        str: Response derived from the most similar chunk in knowledge.
        """
        prompt_embedding = self.get_embedding(prompt)
        df = pd.read_csv(f"embeddings-{self.unique_filename}", encoding='utf-8')
        df['embeddings'] = df['embeddings'].apply(lambda x: np.array(eval(x)))
        df['similarity'] = df['embeddings'].apply(lambda emb: self.calculate_similarity(prompt_embedding, emb))

        best_chunk = df.loc[df['similarity'].idxmax(), 'text']

        client = OpenAI(api_key=self.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"You are {self.persona}, a knowledge-based assistant. Forget previous context."},
                {"role": "user", "content": f"Answer based only on this information: {best_chunk}. Prompt: {prompt}"}
            ],
            temperature=0
        )

        return response.choices[0].message.content


# ---------------------------------------------------------------------------
# EvaluationAgent
# ---------------------------------------------------------------------------
class EvaluationAgent:
    """
    Evaluates a worker agent's answers against criteria and, when an answer fails,
    generates correction instructions and asks the worker to refine it
    (up to `max_interactions` attempts).
    """

    def __init__(self, openai_api_key, persona, evaluation_criteria,
                 worker_agent=None, max_interactions=10, agent_to_evaluate=None):
        """
        `worker_agent` (Phase 1 name) and `agent_to_evaluate` (Phase 2 name) are
        aliases - pass either one. The worker must expose a `respond(prompt)` method.
        """
        self.openai_api_key = openai_api_key
        self.persona = persona
        self.evaluation_criteria = evaluation_criteria
        self.worker_agent = worker_agent if worker_agent is not None else agent_to_evaluate
        if self.worker_agent is None:
            raise ValueError("EvaluationAgent needs a worker agent (worker_agent / agent_to_evaluate).")
        self.max_interactions = max_interactions

    def evaluate(self, initial_prompt):
        """
        Run the worker -> evaluate -> correct loop.

        Returns a dict:
            {"final_response": <worker's last answer>,
             "evaluation":     <evaluator's last verdict>,
             "iterations":     <number of loop iterations performed>}
        """
        client = _make_client(self.openai_api_key)
        prompt_to_evaluate = initial_prompt
        response_from_worker = None
        evaluation = None
        iterations_performed = 0

        for i in range(self.max_interactions):
            iterations_performed = i + 1
            print(f"\n--- Interaction {i+1} ---")

            print(" Step 1: Worker agent generates a response to the prompt")
            print(f"Prompt:\n{prompt_to_evaluate}")
            response_from_worker = self.worker_agent.respond(prompt_to_evaluate)
            print(f"Worker Agent Response:\n{response_from_worker}")

            print(" Step 2: Evaluator agent judges the response")
            eval_prompt = (
                f"Does the following answer: {response_from_worker}\n"
                f"Meet this criteria: {self.evaluation_criteria}\n"
                f"Respond Yes or No, and the reason why it does or doesn't meet the criteria."
            )
            response = client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {"role": "system", "content": self.persona},
                    {"role": "user", "content": eval_prompt},
                ],
                temperature=0,
            )
            evaluation = response.choices[0].message.content.strip()
            print(f"Evaluator Agent Evaluation:\n{evaluation}")

            print(" Step 3: Check if evaluation is positive")
            if evaluation.lower().startswith("yes"):
                print("\u2705 Final solution accepted.")
                break

            if i == self.max_interactions - 1:
                # Out of attempts: no point generating corrections nobody will use.
                print("\u26a0\ufe0f Maximum interactions reached without acceptance.")
                break

            print(" Step 4: Generate instructions to correct the response")
            instruction_prompt = (
                f"Provide instructions to fix an answer based on these reasons why it is incorrect: {evaluation}"
            )
            response = client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {"role": "system", "content": self.persona},
                    {"role": "user", "content": instruction_prompt},
                ],
                temperature=0,
            )
            instructions = response.choices[0].message.content.strip()
            print(f"Instructions to fix:\n{instructions}")

            print(" Step 5: Send feedback to worker agent for refinement")
            prompt_to_evaluate = (
                f"The original prompt was: {initial_prompt}\n"
                f"The response to that prompt was: {response_from_worker}\n"
                f"It has been evaluated as incorrect.\n"
                f"Make only these corrections, do not alter content validity: {instructions}"
            )

        return {
            "final_response": response_from_worker,
            "evaluation": evaluation,
            "iterations": iterations_performed,
        }


# ---------------------------------------------------------------------------
# ScoredEvaluationAgent  (enhancement: rubric-based scoring)
# ---------------------------------------------------------------------------
class ScoredEvaluationAgent(EvaluationAgent):
    """
    An EvaluationAgent that grades the worker's answer against a multi-criteria rubric
    (1-5 per criterion) instead of giving a single Yes/No verdict.

    An answer is accepted when BOTH hold:
        * the average score >= pass_score, and
        * no single criterion scores below min_criterion_score.
    Otherwise the weakest criteria are turned into correction instructions and the worker
    retries (up to max_interactions). It is a drop-in replacement: it exposes the same
    evaluate() method and the returned dict contains the same keys as EvaluationAgent
    ("final_response", "evaluation", "iterations") plus "scores", "average_score", "passed".
    """

    def __init__(self, openai_api_key, persona, evaluation_criteria, worker_agent=None,
                 max_interactions=10, agent_to_evaluate=None, scoring_rubric=None,
                 pass_score=4.0, min_criterion_score=3, reference_context=None):
        """
        scoring_rubric   : {criterion_name: description}. Defaults to one criterion built from
                           `evaluation_criteria`.
        pass_score       : minimum average score (1-5) required to accept.
        min_criterion_score: minimum score every single criterion must reach.
        reference_context: optional source material (e.g. the product spec) the evaluator may
                           use to judge whether the answer is faithful to it.
        """
        super().__init__(openai_api_key, persona, evaluation_criteria,
                         worker_agent, max_interactions, agent_to_evaluate)
        self.scoring_rubric = scoring_rubric or {"criteria_match": evaluation_criteria}
        self.pass_score = pass_score
        self.min_criterion_score = min_criterion_score
        self.reference_context = reference_context

    def _rubric_text(self):
        return "\n".join(f"- {name}: {description}" for name, description in self.scoring_rubric.items())

    def _parse_scores(self, raw_text):
        """Extract ({criterion: int 1-5}, feedback) from the evaluator's JSON; (None, None) if unusable."""
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            return None, None
        try:
            data = json.loads(match.group(0))
            raw_scores = data["scores"]
            scores = {name: max(1, min(5, int(round(float(raw_scores[name]))))) for name in self.scoring_rubric}
        except (ValueError, KeyError, TypeError):
            return None, None
        return scores, str(data.get("feedback", "")).strip()

    def _score_answer(self, client, answer):
        """Ask the LLM to score `answer` (temperature=0). Returns (scores|None, feedback, raw_text)."""
        context_block = (
            f"Reference material (use it to judge whether the answer is faithful to it):\n{self.reference_context}\n\n"
            if self.reference_context else ""
        )
        scoring_prompt = (
            "Score the answer below on each rubric criterion from 1 (very poor) to 5 (excellent).\n"
            f"Overall requirement: {self.evaluation_criteria}\n\n"
            f"Rubric criteria:\n{self._rubric_text()}\n\n"
            f"{context_block}"
            f"Answer to score:\n{answer}\n\n"
            "Respond with ONLY a JSON object and nothing else, in exactly this form: "
            '{"scores": {"<criterion name>": <integer 1-5>, ...}, '
            '"feedback": "<one or two sentences on the weakest criteria and how to improve them>"}'
        )
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": self.persona},
                {"role": "user", "content": scoring_prompt},
            ],
            temperature=0,
        )
        raw_text = response.choices[0].message.content.strip()
        scores, feedback = self._parse_scores(raw_text)
        return scores, feedback, raw_text

    def evaluate(self, initial_prompt):
        """
        Worker -> score -> (correct -> worker ...) loop.

        Returns {"final_response", "evaluation", "iterations", "scores", "average_score", "passed"}.
        """
        client = _make_client(self.openai_api_key)
        prompt_to_evaluate = initial_prompt
        response_from_worker, evaluation = None, None
        scores, average_score, passed, iterations_performed = None, None, False, 0

        for i in range(self.max_interactions):
            iterations_performed = i + 1
            print(f"\n--- Scored interaction {i+1} ---")

            print(" Step 1: Worker agent generates a response to the prompt")
            print(f"Prompt:\n{prompt_to_evaluate}")
            response_from_worker = self.worker_agent.respond(prompt_to_evaluate)
            print(f"Worker Agent Response:\n{response_from_worker}")

            print(" Step 2: Evaluator agent scores the response against the rubric")
            scores, feedback, raw_text = self._score_answer(client, response_from_worker)
            if scores is None:
                # Evaluator output could not be parsed: never accept an unscored answer.
                average_score, passed = None, False
                feedback = "The answer could not be scored; make sure it strictly follows the required structure."
                evaluation = f"Scoring failed (unparseable evaluator output): {raw_text[:200]}"
            else:
                average_score = sum(scores.values()) / len(scores)
                passed = (average_score >= self.pass_score
                          and min(scores.values()) >= self.min_criterion_score)
                evaluation = (f"{'PASS' if passed else 'FAIL'} - average {average_score:.2f}/5 "
                              f"(needs >= {self.pass_score}, every criterion >= {self.min_criterion_score}). "
                              f"Scores: {scores}. Feedback: {feedback}")
            print(f"Evaluator Agent Evaluation:\n{evaluation}")

            print(" Step 3: Check if the scores meet the acceptance thresholds")
            if passed:
                print("\u2705 Final solution accepted.")
                break
            if i == self.max_interactions - 1:
                print("\u26a0\ufe0f Maximum interactions reached without meeting the score thresholds.")
                break

            print(" Step 4: Generate instructions to raise the lowest-scoring criteria")
            instruction_prompt = (
                "Provide concise, concrete instructions to improve an answer so it scores higher on this rubric:\n"
                f"{self._rubric_text()}\n"
                f"Current scores: {scores if scores else 'not available'}\n"
                f"Evaluator feedback: {feedback}\n"
                "Focus on the lowest-scoring criteria."
            )
            response = client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {"role": "system", "content": self.persona},
                    {"role": "user", "content": instruction_prompt},
                ],
                temperature=0,
            )
            instructions = response.choices[0].message.content.strip()
            print(f"Instructions to fix:\n{instructions}")

            print(" Step 5: Send feedback to worker agent for refinement")
            prompt_to_evaluate = (
                f"The original prompt was: {initial_prompt}\n"
                f"The response to that prompt was: {response_from_worker}\n"
                f"It scored below the acceptance thresholds.\n"
                f"Make only these corrections, do not alter content validity: {instructions}"
            )

        return {
            "final_response": response_from_worker,
            "evaluation": evaluation,
            "iterations": iterations_performed,
            "scores": scores,
            "average_score": average_score,
            "passed": passed,
        }


# ---------------------------------------------------------------------------
# RoutingAgent
# ---------------------------------------------------------------------------
class RoutingAgent:
    """
    Routes a prompt to the most suitable agent by comparing the embedding of the
    prompt with the embedding of each agent's description (cosine similarity).

    `agents` is a list of dicts: {"name": str, "description": str, "func": callable(prompt)}.
    """

    def __init__(self, openai_api_key, agents):
        self.openai_api_key = openai_api_key
        self.agents = agents
        self._embedding_cache = {}  # avoid re-embedding the same description on every call

    def get_embedding(self, text):
        """Return the text-embedding-3-large embedding vector for `text`."""
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        client = _make_client(self.openai_api_key)
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
            encoding_format="float",
        )
        embedding = response.data[0].embedding
        self._embedding_cache[text] = embedding
        return embedding

    def route(self, user_input):
        """Pick the best-matching agent for `user_input`, call it, and return its response."""
        input_emb = self.get_embedding(user_input)
        best_agent = None
        best_score = -1

        for agent in self.agents:
            agent_emb = self.get_embedding(agent["description"])
            if agent_emb is None:
                continue

            similarity = np.dot(input_emb, agent_emb) / (np.linalg.norm(input_emb) * np.linalg.norm(agent_emb))
            print(f"[Router] similarity with '{agent['name']}': {similarity:.3f}")

            if similarity > best_score:
                best_score = similarity
                best_agent = agent

        if best_agent is None:
            return "Sorry, no suitable agent could be selected."

        print(f"[Router] Best agent: {best_agent['name']} (score={best_score:.3f})")
        return best_agent["func"](user_input)


# ---------------------------------------------------------------------------
# ActionPlanningAgent
# ---------------------------------------------------------------------------
class ActionPlanningAgent:
    """Uses its knowledge to turn a user prompt into an ordered list of action steps."""

    def __init__(self, openai_api_key, knowledge):
        self.openai_api_key = openai_api_key
        self.knowledge = knowledge

    def extract_steps_from_prompt(self, prompt):
        """Return a clean list of step strings for the task described in `prompt`."""
        client = _make_client(self.openai_api_key)
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an action planning agent. Using your knowledge, you extract from the user "
                        "prompt the steps requested to complete the action the user is asking for. "
                        "You return the steps as a list. Only return the steps in your knowledge. "
                        f"Forget any previous context. This is your knowledge: {self.knowledge}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        response_text = response.choices[0].message.content

        # Clean: split into lines, drop bullets/numbering, discard empty lines.
        steps = []
        for line in response_text.split("\n"):
            cleaned = re.sub(r"^\s*(?:\d+[\.\)]|[-*\u2022])\s*", "", line).strip()
            if cleaned:
                steps.append(cleaned)
        return steps
