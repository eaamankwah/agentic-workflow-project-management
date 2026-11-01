# Test script for DirectPromptAgent class

from workflow_agents.base_agents import DirectPromptAgent
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Load the OpenAI API key from the environment variables
openai_api_key = os.getenv("OPENAI_API_KEY")

prompt = "What is the Capital of France?"

# Instantiate the DirectPromptAgent as direct_agent
direct_agent = DirectPromptAgent(openai_api_key)

# Use direct_agent to send the prompt defined above and store the response
print(f"Prompt: {prompt}")
direct_agent_response = direct_agent.respond(prompt)

# Print the response from the agent
print(f"Response: {direct_agent_response}")

# Explain the knowledge source used by the agent
print(
    "\nKnowledge source: the DirectPromptAgent sends the prompt to the LLM with no system prompt, "
    "persona or extra context, so the answer comes solely from the general knowledge the "
    "gpt-3.5-turbo model learned during its training."
)
