#!/bin/bash
cd /Volumes/MyData/.openclaw/workspace-anthony/linyan
source .venv/bin/activate
export DATABASE_URL="postgresql://mengwee:hoccisFau8899!@localhost:5432/yourdb"
export ARK_API_KEY="ark-07…f689"
export ARK_API_BASE="https://ark.ap-southeast.bytepluses.com/api/v3"
python3 app.py 2>&1