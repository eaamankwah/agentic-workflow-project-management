# Test script for KnowledgeAugmentedPromptAgent class

from workflow_agents.base_agents import KnowledgeAugmentedPromptAgent
import os
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# Define the parameters for the agent
openai_api_key = os.getenv("OPENAI_API_KEY")

prompt = "What is the capital of France?"

persona = "You are a college professor, your answer always starts with: Dear students,"
# Deliberately WRONG knowledge, to prove the agent uses the supplied knowledge and not its own
knowledge = "The capital of France is London, not Paris"

# Instantiate a KnowledgeAugmentedPromptAgent with the persona and knowledge above
knowledge_agent = KnowledgeAugmentedPromptAgent(openai_api_key, persona, knowledge)

print(f"Prompt: {prompt}")
knowledge_agent_response = knowledge_agent.respond(prompt)
print(f"Response: {knowledge_agent_response}")

# Demonstrate that the agent used the provided knowledge rather than its inherent LLM knowledge
print(
    "\nThe agent was told 'The capital of France is London, not Paris'. Because it must answer "
    "ONLY from the provided knowledge, it answers with London instead of the (true) answer from "
    "its own training data, Paris."
)
if "london" in knowledge_agent_response.lower():
    print("CONFIRMED: the response uses the provided knowledge (London), not the LLM's own knowledge.")
else:
    print("WARNING: the response does not mention London - the provided knowledge may not have been used.")
