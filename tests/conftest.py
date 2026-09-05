"""SECTION 19 AUDIT: Shared pytest fixtures for Sweeper Bot V2 tests."""
import sys
import os
import pytest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import SweeperConfig, BotState
from modules.safety_rails import SafetyRails


@pytest.fixture
def config():
    return SweeperConfig(paper_mode=True)


@pytest.fixture
def safety(config):
    return SafetyRails(config)
