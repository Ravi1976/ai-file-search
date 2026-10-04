# AI File Search

Find any file or folder on C:, D: or anywhere on your computer, by name, by typo-tolerant match,
by plain-English description, or by text inside documents.

## Setup
    python -m venv venv
    venv\Scripts\activate
    pip install -r requirements.txt

## Use
    python ai_file_search.py index                         # scan all drives (first time, then occasionally)
    python ai_file_search.py find "resume 2026.pdf"
    python ai_file_search.py find "invoices" --type folder
    python ai_file_search.py ask "my latest resume pdf"    # --provider ollama | anthropic for real AI
    python ai_file_search.py content "loan provisioning"   # search inside documents
    python ai_file_search.py                               # interactive mode
