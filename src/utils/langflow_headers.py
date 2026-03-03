"""Utility functions for building Langflow request headers."""

from typing import Dict
from utils.container_utils import transform_localhost_url


def add_provider_credentials_to_headers(headers: Dict[str, str], config) -> None:
    """Add provider credentials to headers as Langflow global variables.
    
    Args:
        headers: Dictionary of headers to add credentials to
        config: OpenRAGConfig object containing provider configurations
    """
    # Add Ollama endpoint (with localhost transformation) — always needed
    if config.providers.ollama.endpoint:
        ollama_endpoint = transform_localhost_url(config.providers.ollama.endpoint)
        headers["X-LANGFLOW-GLOBAL-VAR-OLLAMA_BASE_URL"] = str(ollama_endpoint)

    # In airgap mode, explicitly blank every cloud API key so that the
    # per-request header override takes precedence over the container's
    # env-var defaults (e.g. OPENAI_API_KEY=None which is truthy).
    # An empty value makes `if not api_key:` true in the Langflow
    # EmbeddingModel component, so fail-safe nodes return None and
    # no OpenAI / cloud client objects are ever constructed.
    if getattr(config, "airgap", False):
        headers["X-LANGFLOW-GLOBAL-VAR-OPENAI_API_KEY"] = ""
        headers["X-LANGFLOW-GLOBAL-VAR-ANTHROPIC_API_KEY"] = ""
        headers["X-LANGFLOW-GLOBAL-VAR-WATSONX_API_KEY"] = ""
        headers["X-LANGFLOW-GLOBAL-VAR-WATSONX_ENDPOINT"] = ""
        headers["X-LANGFLOW-GLOBAL-VAR-WATSONX_PROJECT_ID"] = ""
        return

    # Add OpenAI credentials
    if config.providers.openai.api_key:
        headers["X-LANGFLOW-GLOBAL-VAR-OPENAI_API_KEY"] = str(config.providers.openai.api_key)
    
    # Add Anthropic credentials
    if config.providers.anthropic.api_key:
        headers["X-LANGFLOW-GLOBAL-VAR-ANTHROPIC_API_KEY"] = str(config.providers.anthropic.api_key)
    
    # Add WatsonX credentials
    if config.providers.watsonx.api_key:
        headers["X-LANGFLOW-GLOBAL-VAR-WATSONX_API_KEY"] = str(config.providers.watsonx.api_key)
    
    if config.providers.watsonx.project_id:
        headers["X-LANGFLOW-GLOBAL-VAR-WATSONX_PROJECT_ID"] = str(config.providers.watsonx.project_id)


def build_mcp_global_vars_from_config(config) -> Dict[str, str]:
    """Build MCP global variables dictionary from OpenRAG configuration.
    
    Args:
        config: OpenRAGConfig object containing provider configurations
        
    Returns:
        Dictionary of global variables for MCP servers (without X-Langflow-Global-Var prefix)
    """
    global_vars = {}
    
    # Add Ollama endpoint (with localhost transformation) — always needed
    if config.providers.ollama.endpoint:
        ollama_endpoint = transform_localhost_url(config.providers.ollama.endpoint)
        global_vars["OLLAMA_BASE_URL"] = ollama_endpoint
    
    # Add selected embedding model
    if config.knowledge.embedding_model:
        global_vars["SELECTED_EMBEDDING_MODEL"] = config.knowledge.embedding_model

    # In airgap mode, blank all cloud provider credentials
    if getattr(config, "airgap", False):
        global_vars["OPENAI_API_KEY"] = ""
        global_vars["ANTHROPIC_API_KEY"] = ""
        global_vars["WATSONX_API_KEY"] = ""
        global_vars["WATSONX_ENDPOINT"] = ""
        global_vars["WATSONX_PROJECT_ID"] = ""
        return global_vars

    # Add OpenAI credentials
    if config.providers.openai.api_key:
        global_vars["OPENAI_API_KEY"] = config.providers.openai.api_key
    
    # Add Anthropic credentials
    if config.providers.anthropic.api_key:
        global_vars["ANTHROPIC_API_KEY"] = config.providers.anthropic.api_key
    
    # Add WatsonX credentials
    if config.providers.watsonx.api_key:
        global_vars["WATSONX_API_KEY"] = config.providers.watsonx.api_key
    
    if config.providers.watsonx.project_id:
        global_vars["WATSONX_PROJECT_ID"] = config.providers.watsonx.project_id
    
    return global_vars

