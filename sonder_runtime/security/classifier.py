"""Middleware security classifier for tool calls."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sonder_runtime.security.classifier")

class SecurityClassifier:
    """Classifies tool calls for restricted models using a model or static rules."""
    
    def __init__(self):
        self.restricted_prefixes = ("astra-cyber-",)

    def is_restricted(self, model_name: str) -> bool:
        return any(model_name.startswith(p) for p in self.restricted_prefixes)

    def classify(self, model_name: str, tool_name: str, arguments: Any) -> str:
        """
        Return 'ALLOW', 'ASK', or 'DENY'.
        """
        if not self.is_restricted(model_name):
            return "ALLOW"
        
        # 1. Static Rule-Based Classifier (Safety First)
        if tool_name == "git_commit":
            return "ALLOW"
        
        if tool_name in {"file_delete", "run_command"}:
            # Elevated or destructive tools require prompt
            return "ASK"
            
        # 2. Model-Based Classifier (For Complex/Ambiguous cases)
        # Placeholder: Wire up to qwen3.5:4b for analysis
        logger.info(f"Classifier analyzing {tool_name} for model {model_name}")
        
        # Example decision logic (would call sonder RPC here)
        # For now, default to ASK to be safe
        return "ASK"
