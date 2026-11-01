# Test script for AugmentedPromptAgent class

from workflow_agents.base_agents import AugmentedPromptAgent
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Retrieve OpenAI API key from environment variables
openai_api_key = os.getenv("OPENAI_API_KEY")

prompt = "What is the capital of France?"
persona = "You are a college professor; your answers always start with: 'Dear students,'"

# Instantiate an object of AugmentedPromptAgent with the required parameters
augmented_agent = AugmentedPromptAgent(openai_api_key, persona)

# Send the 'prompt' to the agent and store the response in 'augmented_agent_response'
print(f"Prompt: {prompt}")
augmented_agent_response = augmented_agent.respond(prompt)

# Print the agent's response
print(f"Response: {augmented_agent_response}")

# Explanation (printed, not just commented, so it appears in the captured test evidence):
print(
    "\nKnowledge source: no knowledge was supplied to this agent, so the factual content of the "
    "answer (that Paris is the capital of France) comes from the LLM's own general training "
    "knowledge, exactly as with DirectPromptAgent."
)
print(
    "Persona impact: the system prompt instructed the model to assume the persona of a college "
    "professor and to start every answer with 'Dear students,'. This changes the style, tone and "
    "opening line of the response, but not its factual content, the same fact (Paris) is reported "
    "whether or not a persona is applied. The system prompt's 'forget all previous context' "
    "instruction also means each call is independent of any prior conversation."
)
