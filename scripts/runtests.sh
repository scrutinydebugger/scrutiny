#!/bin/bash
set -euxo pipefail

SCRUTINY_COVERAGE_SUFFIX="${SCRUTINY_COVERAGE_SUFFIX:-dev}"

HTML_COVDIR="htmlcov_${SCRUTINY_COVERAGE_SUFFIX}"
COV_DATAFILE=".coverage_${SCRUTINY_COVERAGE_SUFFIX}"

python3 -m coverage run --data-file ${COV_DATAFILE} -m scrutiny runtest
python3 -m mypy scrutiny
python3 -m coverage report --data-file ${COV_DATAFILE}
python3 -m coverage html --data-file ${COV_DATAFILE} -d $HTML_COVDIR
