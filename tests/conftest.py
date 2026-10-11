import pytest

from analyst_agent.config import ROOT
from analyst_agent.safety.policy import load_policy
from analyst_agent.session import DataScope

CONFIG = ROOT / "config"


@pytest.fixture(scope="session")
def policy():
    return load_policy(CONFIG / "data_policy.yaml")


@pytest.fixture
def brand_scope():
    return DataScope(filters={"brand": ("MG", "Nike")})


@pytest.fixture
def all_scope():
    return DataScope(all_data=True)
