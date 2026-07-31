import os

# Verifies hooks/report_git_branch.py's Authorization header against
# LiteLLM's /key/info (server.py receive_git_branch).
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
