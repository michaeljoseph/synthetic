#!/usr/bin/env bash

this_dir=$(dirname "${BASH_SOURCE[0]}")
source ${this_dir}/../../demo-magic/demo-magic.sh
source ${this_dir}/../demo-env

PROMPT_TIMEOUT=1
TYPE_SPEED=50

clear
p "# 🤖 synthetic: toggl.com timesheets and slack standup notes (from markdown)"

pe "synthetic --help"

pe "synthetic store"

pe "synthetic slack"

p "# 🤖🙌👍👋"
