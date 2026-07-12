"""Train a QLoRA adapter selected by Sonder Runtime.

The heavy ML dependencies remain deferred so this module can be imported and
validated on normal runtime installs. Training is accepted only when the
confirmed lifecycle controller has created a fresh, one-use training plan.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASE = os