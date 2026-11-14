"""test configs"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def change_test_dir(request):
    """change directory to project folder"""
    backend_dir = request.config.rootdir / "backend"
    if os.path.isdir(backend_dir):
        os.chdir(backend_dir)
