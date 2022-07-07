#!/bin/bash
set -euo pipefail

DIRS=("test" "scrutiny")

PROJECT_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/.. >/dev/null 2>&1 && pwd -P )"

for dir in ${DIRS[*]}; do
    autopep8 $PROJECT_ROOT/$dir --recursive --in-place --max-line-length 150 --select E101,E11,E121,E122,E123,E124,E125,E126,E127,E128,E129,E131,E133,E20,E211,E22,E224,E225,E226,E227,E228,E231,E241,E242,E251,E252,E26,E265,E27,E301,E302,E303,E304,E305,E306,E401,E501,E502,W291,W292,W293,W391,W503,W504,W603 
done