#!/usr/bin/env bash

# install packages

virtualenv perf --python=python2
. ./perf/bin/activate


pip install `ls azure_cli_*.whl`
pip install `ls azure_cli-*.whl`

