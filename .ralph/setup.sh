#!/bin/bash
# Runs at the start of every ralph-afk container: installs the project's test dependencies.
pip install --user -q -r domain-overview/requirements-dev.txt
